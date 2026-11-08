"""Build the synthetic warehouse: 13 source tables loaded daily for 120 days, 28 downstream models, a load log,
schema snapshots, and 8 planted incidents with ground truth.

Randomness is seeded. Row values come from DuckDB `hash()` of (seed, key, salt), so the same seed always gives the
same warehouse no matter how many threads DuckDB uses. Daily row counts come from Python's seeded `random`.
"""

from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from .config import Config

SEED = 7

# Day of week factors, Monday first. Month-end and holiday multipliers apply on top.
PROFILES = {
    "commerce": {"dow": [1.05, 1.0, 1.0, 1.02, 1.08, 0.72, 0.62], "month_end": 1.55, "holiday": 0.45},
    "web": {"dow": [0.95, 0.95, 0.95, 0.97, 1.0, 1.2, 1.25], "month_end": 1.25, "holiday": 0.8},
    "signups": {"dow": [1.0, 1.0, 1.0, 1.0, 1.05, 0.8, 0.75], "month_end": 1.3, "holiday": 0.6},
    "support": {"dow": [1.15, 1.1, 1.05, 1.05, 1.0, 0.45, 0.35], "month_end": 1.0, "holiday": 0.3},
}

# Typical arrival time of each source, in hours after midnight of the day after the partition date.
ARRIVAL_HOURS = {
    "raw_fx_rates": 0.8, "raw_products": 1.0, "raw_customers": 1.5, "raw_orders": 2.0, "raw_order_items": 2.1,
    "raw_payments": 2.5, "raw_refunds": 3.0, "raw_inventory": 3.0, "raw_shipments": 3.5, "raw_returns": 4.0,
    "raw_web_sessions": 4.5, "raw_support_tickets": 5.0, "raw_marketing_spend": 6.5,
}


@dataclass
class PlantedIncident:
    id: str
    kind: str
    table: str
    column: str | None
    start_day: int
    end_day: int
    monitors: list[str]
    description: str
    affected: list[str] = field(default_factory=list)  # root plus lineage descendants, filled in at build time

    def to_dict(self) -> dict:
        return asdict(self)


PLANTED = [
    PlantedIncident("partial_load", "volume drop", "raw_web_sessions", None, 45, 45, ["volume"],
                    "The analytics export timed out and loaded 35% of the day's sessions."),
    PlantedIncident("late_load", "freshness", "raw_inventory", None, 52, 52, ["freshness"],
                    "The warehouse system snapshot arrived 10 hours late."),
    PlantedIncident("schema_change", "schema", "raw_customers", "postal_code", 58, 58, ["schema"],
                    "postal_code changed from INTEGER to VARCHAR to support Canadian and UK codes."),
    PlantedIncident("duplicate_load", "volume spike", "raw_shipments", None, 66, 66, ["volume", "uniqueness"],
                    "A connector retry loaded the same shipment file twice."),
    PlantedIncident("cents_shift", "distribution shift", "raw_payments", "amount", 74, 76, ["distribution"],
                    "The processor export started sending amounts in cents instead of dollars."),
    PlantedIncident("null_spike", "null rate", "raw_customers", "email", 84, 85, ["null_rate"],
                    "A signup form change left email empty for about 35% of new customers."),
    PlantedIncident("orphan_returns", "referential", "raw_returns", "order_id", 102, 103, ["referential"],
                    "The returns portal wrote marketplace order ids that do not exist in the orders table."),
    PlantedIncident("silent_quantity_bug", "silent upstream bug", "raw_order_items", "quantity", 108, 111,
                    ["distribution"], "A storefront release wrote quantity 1 on every order line; "
                    "units and revenue are understated in every downstream model."),
]


def U(salt: str, key: str = "d, i") -> str:
    """Uniform [0, 1) from a hash of the key and salt."""
    return f"((hash({SEED}, {key}, '{salt}') % 1000003) / 1000003.0)"


def N(salt: str, key: str = "d, i") -> str:
    """Approximately standard normal (Irwin-Hall with 4 uniforms)."""
    parts = " + ".join(U(f"{salt}{j}", key) for j in range(4))
    return f"(({parts}) - 2.0) * 1.7320508"


def cat(u: str, options: list[tuple[str, float]], quote: bool = True) -> str:
    acc, whens = 0.0, []
    for val, w in options[:-1]:
        acc += w
        v = f"'{val}'" if quote else str(val)
        whens.append(f"WHEN {u} < {acc:.4f} THEN {v}")
    last = f"'{options[-1][0]}'" if quote else str(options[-1][0])
    return f"CASE {' '.join(whens)} ELSE {last} END"


def counts_frame(cfg: Config, scale: float, rng: random.Random) -> dict[str, list[int]]:
    """Rows per day for each generated source, with weekday, month-end, holiday, trend, and noise."""

    def series(base: float, profile: str) -> list[int]:
        p = PROFILES[profile]
        out = []
        for d in range(cfg.days):
            f = p["dow"][cfg.day_date(d).weekday()] * (1 + 0.002 * d)
            if cfg.is_month_end(d):
                f *= p["month_end"]
            if cfg.is_holiday(d):
                f *= p["holiday"]
            f *= math.exp(rng.gauss(0, 0.04))
            out.append(max(1, round(base * scale * f)))
        return out

    orders = series(500, "commerce")
    return {
        "customers": series(80, "signups"),
        "orders": orders,
        "items": [max(1, round(n * 2.2 * math.exp(rng.gauss(0, 0.02)))) for n in orders],
        "web": series(4000, "web"),
        "support": series(70, "support"),
        "products": [200 + d // 10 for d in range(cfg.days)],
    }


def raw_sql() -> dict[str, str]:
    """SELECT statements for each source table. `_counts` has one row per day with n_* columns."""
    base = "SELECT c.*, unnest(range(c.{n})) AS i FROM _counts c"
    ts = lambda salt: f"load_date::TIMESTAMP + to_seconds(floor({U(salt)} * 86399)::BIGINT)"  # noqa: E731
    return {
        "raw_fx_rates": f"""
            WITH b AS ({base.format(n='n_fx')})
            SELECT ['USD','CAD','GBP','EUR'][i + 1] AS currency,
                   CASE WHEN i = 0 THEN 1.0 ELSE round([1.0, 0.73, 1.27, 1.08][i + 1] * (1 + 0.004 * {N('fx')}), 5)
                   END AS rate_to_usd, load_date
            FROM b""",
        "raw_products": f"""
            WITH b AS ({base.format(n='n_products')})
            SELECT (i + 1)::BIGINT AS product_id, 'SKU-' || lpad((i + 1)::VARCHAR, 5, '0') AS sku,
                   {cat(U('cat', 'i'), [('tents', .2), ('packs', .2), ('apparel', .3), ('footwear', .15),
                                        ('cooking', .15)])} AS category,
                   round(8 + exp(3.3 + 0.6 * {N('price', 'i')}) * (1 + 0.01 * ({U('pd')} - 0.5)), 2) AS unit_price,
                   (100 + {U('w', 'i')} * 4000)::INTEGER AS weight_grams, load_date
            FROM b""",
        "raw_customers": f"""
            WITH b AS ({base.format(n='n_customers')})
            SELECT (cum_customers + i)::BIGINT AS customer_id,
                   CASE WHEN {U('em')} < 0.01 THEN NULL ELSE 'c' || (cum_customers + i) || '@mail.example' END AS email,
                   {cat(U('ctry'), [('US', .6), ('CA', .1), ('GB', .15), ('DE', .15)])} AS country,
                   (10000 + {U('zip')} * 89999)::INTEGER AS postal_code,
                   {cat(U('sch'), [('organic', .45), ('paid', .3), ('referral', .15), ('social', .1)])}
                     AS signup_channel,
                   {ts('cts')} AS created_at, load_date
            FROM b""",
        "raw_orders": f"""
            WITH b AS ({base.format(n='n_orders')})
            SELECT (d * 1000000 + i)::BIGINT AS order_id,
                   floor({U('cust')} * (cum_customers + n_customers))::BIGINT AS customer_id,
                   {ts('ots')} AS order_ts,
                   {cat(U('cur'), [('USD', .6), ('CAD', .1), ('GBP', .15), ('EUR', .15)])} AS currency,
                   {cat(U('st'), [('completed', .93), ('pending', .05), ('test', .02)])} AS status,
                   {cat(U('ch'), [('web', .7), ('app', .3)])} AS channel,
                   CASE WHEN {U('fs')} < 0.4 THEN 0.0 ELSE round(4.99 + {U('fee')} * 10, 2) END AS shipping_fee,
                   load_date
            FROM b""",
        "raw_order_items": f"""
            WITH b AS ({base.format(n='n_items')}),
            l AS (SELECT (d * 10000000 + i)::BIGINT AS order_item_id,
                         (d * 1000000 + floor({U('oi')} * n_orders))::BIGINT AS order_id,
                         (1 + floor({U('pi')} * 200))::BIGINT AS product_id,
                         {cat(U('q'), [(1, .4), (2, .3), (3, .2), (4, .1)], quote=False)}::INTEGER AS quantity,
                         CASE WHEN {U('dc')} < 0.7 THEN 0.0 ELSE 0.05 + floor({U('dv')} * 4) * 0.05 END
                           AS discount_pct,
                         load_date FROM b)
            SELECT l.order_item_id, l.order_id, l.product_id, l.quantity, p.unit_price,
                   round(l.discount_pct, 2) AS discount_pct, l.load_date
            FROM l JOIN raw_products p USING (product_id, load_date)""",
        "raw_payments": f"""
            SELECT (order_id + 500000000000)::BIGINT AS payment_id, order_id,
                   round(exp(4.2 + 0.6 * {N('amt', 'order_id')}), 2) AS amount,
                   {cat(U('pm', 'order_id'), [('card', .7), ('paypal', .2), ('giftcard', .05), ('applepay', .05)])}
                     AS method,
                   currency, load_date
            FROM raw_orders WHERE status <> 'test' AND {U('pay', 'order_id')} > 0.02""",
        "raw_refunds": f"""
            SELECT (order_id + 600000000000)::BIGINT AS refund_id, order_id,
                   round(10 + {U('ra', 'order_id')} * 80, 2) AS amount,
                   {cat(U('rr', 'order_id'), [('damaged', .3), ('late', .3), ('changed_mind', .4)])} AS reason,
                   load_date
            FROM raw_orders WHERE status = 'completed' AND {U('ref', 'order_id')} < 0.04""",
        "raw_shipments": f"""
            SELECT (order_id + 700000000000)::BIGINT AS shipment_id, order_id,
                   {cat(U('car', 'order_id'), [('UPS', .4), ('FedEx', .3), ('USPS', .3)])} AS carrier,
                   {cat(U('wh', 'order_id'), [('reno', .4), ('columbus', .35), ('allentown', .25)])} AS warehouse,
                   order_ts + to_seconds(floor(3600 * (6 + {U('sh', 'order_id')} * 40))::BIGINT) AS shipped_at,
                   load_date
            FROM raw_orders WHERE status = 'completed' AND {U('ship', 'order_id')} < 0.95""",
        "raw_returns": f"""
            SELECT (order_id + 800000000000)::BIGINT AS return_id, order_id,
                   {cat(U('rtr', 'order_id'), [('size', .45), ('defect', .2), ('not_as_described', .15),
                                               ('other', .2)])} AS reason,
                   round(15 + {U('rta', 'order_id')} * 120, 2) AS refund_amount, load_date
            FROM raw_orders WHERE status = 'completed' AND {U('ret', 'order_id')} < 0.08""",
        "raw_inventory": f"""
            WITH b AS ({base.format(n='n_inventory')})
            SELECT (1 + i // 3)::BIGINT AS product_id, ['reno','columbus','allentown'][i % 3 + 1] AS warehouse,
                   (20 + {U('oh', 'i')} * 500 * (0.9 + 0.2 * {U('ohd')}))::INTEGER AS on_hand, load_date
            FROM b""",
        "raw_web_sessions": f"""
            WITH b AS ({base.format(n='n_web')})
            SELECT (d * 10000000 + i)::BIGINT AS session_id,
                   CASE WHEN {U('anon')} < 0.4 THEN NULL
                        ELSE floor({U('wc')} * (cum_customers + n_customers))::BIGINT END AS customer_id,
                   {cat(U('dev'), [('desktop', .45), ('mobile', .45), ('tablet', .1)])} AS device,
                   'p/' || floor(pow({U('lp')}, 2) * 300)::INTEGER AS landing_page,
                   (1 + floor(-ln(1 - {U('pv')}) * 4))::INTEGER AS pageviews,
                   {cat(U('rf'), [('google', .4), ('direct', .25), ('email', .15), ('social', .12), ('partner', .08)])}
                     AS referrer, load_date
            FROM b""",
        "raw_marketing_spend": f"""
            WITH b AS ({base.format(n='n_campaigns')}),
            s AS (SELECT i, load_date, d,
                         round((50 + {U('sp', 'i')} * 400) * (0.85 + 0.3 * {U('spd')}), 2) AS spend FROM b)
            SELECT (i + 1)::BIGINT AS campaign_id,
                   {cat(U('mch', 'i'), [('search', .35), ('social', .3), ('display', .2), ('email', .15)])} AS channel,
                   spend, (spend * (0.8 + 0.4 * {U('clk')}))::INTEGER AS clicks, load_date
            FROM s""",
        "raw_support_tickets": f"""
            WITH b AS ({base.format(n='n_support')})
            SELECT (d * 1000000 + i)::BIGINT AS ticket_id,
                   floor({U('tc')} * (cum_customers + n_customers))::BIGINT AS customer_id,
                   {cat(U('tcat'), [('shipping', .35), ('returns', .25), ('product', .2), ('billing', .15),
                                    ('account', .05)])} AS category,
                   {cat(U('tp'), [('low', .5), ('normal', .4), ('high', .1)])} AS priority,
                   {cat(U('tch'), [('email', .5), ('chat', .4), ('phone', .1)])} AS channel, load_date
            FROM b""",
    }


# Downstream models. Each is computed over all partitions at once; every model keeps `load_date`.
TRANSFORMS: dict[str, str] = {
    "stg_customers": """SELECT customer_id, lower(email) AS email, country, CAST(postal_code AS VARCHAR) AS postal_code,
        signup_channel, created_at, load_date FROM raw_customers""",
    "stg_products": "SELECT product_id, sku, category, unit_price, weight_grams, load_date FROM raw_products",
    "stg_orders": """SELECT order_id, customer_id, order_ts, currency, status, channel, shipping_fee, load_date
        FROM raw_orders WHERE status <> 'test'""",
    "stg_order_items": """SELECT order_item_id, order_id, product_id, quantity, unit_price, discount_pct,
        round(quantity * unit_price * (1 - discount_pct), 2) AS line_amount, load_date FROM raw_order_items""",
    "stg_fx_rates": "SELECT currency, rate_to_usd, load_date FROM raw_fx_rates",
    "stg_payments": """SELECT p.payment_id, p.order_id, p.method, p.currency, p.amount,
        round(p.amount * f.rate_to_usd, 2) AS amount_usd, p.load_date
        FROM raw_payments p JOIN stg_fx_rates f USING (currency, load_date)""",
    "stg_refunds": "SELECT refund_id, order_id, amount, reason, load_date FROM raw_refunds",
    "stg_shipments": """SELECT DISTINCT shipment_id, order_id, carrier, warehouse, shipped_at, load_date
        FROM raw_shipments""",
    "stg_returns": "SELECT return_id, order_id, reason, refund_amount, load_date FROM raw_returns",
    "stg_inventory": "SELECT product_id, warehouse, on_hand, load_date FROM raw_inventory",
    "stg_web_sessions": """SELECT session_id, customer_id, device, landing_page, pageviews, load_date
        FROM raw_web_sessions""",
    "stg_marketing_spend": "SELECT campaign_id, channel, spend, clicks, load_date FROM raw_marketing_spend",
    "stg_support_tickets": """SELECT ticket_id, customer_id, category, priority, channel, load_date
        FROM raw_support_tickets""",
    "int_order_revenue": """SELECT o.order_id, any_value(o.customer_id) AS customer_id,
        any_value(o.currency) AS currency,
        count(*)::BIGINT AS items, sum(i.quantity)::BIGINT AS units,
        round(sum(i.quantity * i.unit_price) * any_value(f.rate_to_usd), 2) AS gross_usd,
        round(sum(i.line_amount) * any_value(f.rate_to_usd), 2) AS net_usd, o.load_date
        FROM stg_orders o JOIN stg_order_items i ON i.order_id = o.order_id AND i.load_date = o.load_date
        JOIN stg_fx_rates f ON f.currency = o.currency AND f.load_date = o.load_date
        GROUP BY o.order_id, o.load_date""",
    "int_fulfillment": """SELECT o.order_id, s.carrier, s.warehouse,
        date_diff('minute', o.order_ts, s.shipped_at) / 60.0 AS hours_to_ship, r.order_id IS NOT NULL AS returned,
        o.load_date
        FROM stg_orders o LEFT JOIN stg_shipments s ON s.order_id = o.order_id
        LEFT JOIN (SELECT DISTINCT order_id FROM stg_returns) r ON r.order_id = o.order_id""",
    "fct_orders": """SELECT r.order_id, r.customer_id, r.units, r.net_usd, p.amount_usd AS paid_usd,
        p.method AS payment_method, r.load_date
        FROM int_order_revenue r LEFT JOIN stg_payments p ON p.order_id = r.order_id""",
    "fct_revenue_daily": """SELECT currency, count(*)::BIGINT AS orders, sum(units)::BIGINT AS units,
        round(sum(gross_usd), 2) AS gross_usd, round(sum(net_usd), 2) AS net_usd, load_date
        FROM int_order_revenue GROUP BY load_date, currency""",
    "fct_payments": """SELECT method, count(*)::BIGINT AS payments, round(sum(amount_usd), 2) AS amount_usd,
        round(avg(amount_usd), 2) AS avg_amount_usd, load_date FROM stg_payments GROUP BY load_date, method""",
    "dim_customers": """SELECT customer_id, email, country, signup_channel, created_at, load_date
        FROM stg_customers""",
    "dim_products": "SELECT product_id, sku, category, unit_price, load_date FROM stg_products",
    "fct_inventory_daily": """SELECT p.category, sum(i.on_hand)::BIGINT AS on_hand_units, count(*)::BIGINT AS skus,
        i.load_date FROM stg_inventory i JOIN dim_products p USING (product_id, load_date)
        GROUP BY i.load_date, p.category""",
    "fct_shipments": """SELECT carrier, count(*)::BIGINT AS shipments, avg(hours_to_ship) AS avg_hours_to_ship,
        avg(returned::INTEGER) AS return_rate, load_date
        FROM int_fulfillment WHERE carrier IS NOT NULL GROUP BY load_date, carrier""",
    "fct_returns": """SELECT reason, count(*)::BIGINT AS returns, round(sum(refund_amount), 2) AS refund_amount,
        load_date FROM stg_returns GROUP BY load_date, reason""",
    "fct_web_traffic": """SELECT device, count(*)::BIGINT AS sessions, sum(pageviews)::BIGINT AS pageviews,
        avg((customer_id IS NOT NULL)::INTEGER) AS logged_in_rate, load_date
        FROM stg_web_sessions GROUP BY load_date, device""",
    "fct_marketing_roi": """SELECT m.channel, round(sum(m.spend), 2) AS spend, sum(m.clicks)::BIGINT AS clicks,
        any_value(w.sessions)::BIGINT AS sessions, m.load_date
        FROM stg_marketing_spend m
        JOIN (SELECT load_date, sum(sessions) AS sessions FROM fct_web_traffic GROUP BY load_date) w USING (load_date)
        GROUP BY m.load_date, m.channel""",
    "fct_support_daily": """SELECT category, count(*)::BIGINT AS tickets,
        avg((priority = 'high')::INTEGER) AS high_priority_rate, load_date
        FROM stg_support_tickets GROUP BY load_date, category""",
    "mart_finance_summary": """SELECT r.orders, r.units, r.gross_revenue_usd, r.net_revenue_usd,
        coalesce(p.payments_usd, 0) AS payments_usd, coalesce(f.refunds_usd, 0) AS refunds_usd, r.load_date
        FROM (SELECT load_date, sum(orders)::BIGINT AS orders, sum(units)::BIGINT AS units,
                     round(sum(gross_usd), 2) AS gross_revenue_usd, round(sum(net_usd), 2) AS net_revenue_usd
              FROM fct_revenue_daily GROUP BY load_date) r
        LEFT JOIN (SELECT load_date, round(sum(amount_usd), 2) AS payments_usd FROM fct_payments GROUP BY load_date) p
          USING (load_date)
        LEFT JOIN (SELECT load_date, round(sum(amount), 2) AS refunds_usd FROM stg_refunds GROUP BY load_date) f
          USING (load_date)""",
    "mart_exec_kpis": """SELECT f.net_revenue_usd, f.units, w.sessions::BIGINT AS sessions,
        f.orders / w.sessions AS conversion_rate, coalesce(c.new_customers, 0)::BIGINT AS new_customers, f.load_date
        FROM mart_finance_summary f
        JOIN (SELECT load_date, sum(sessions) AS sessions FROM fct_web_traffic GROUP BY load_date) w USING (load_date)
        LEFT JOIN (SELECT load_date, count(*) AS new_customers FROM dim_customers GROUP BY load_date) c
          USING (load_date)""",
}


def apply_data_incidents(con: duckdb.DuckDBPyConnection, cfg: Config) -> None:
    """Corrupt source partitions the way real incidents do. Freshness and schema incidents are applied to the
    load log and schema snapshots instead."""
    by = {p.id: p for p in PLANTED}

    def dates(p: PlantedIncident) -> str:
        return ", ".join(f"DATE '{cfg.day_date(d)}'" for d in range(p.start_day, p.end_day + 1))

    p = by["partial_load"]
    con.execute(f"DELETE FROM raw_web_sessions WHERE load_date IN ({dates(p)}) AND {U('partial', 'session_id')} < 0.65")
    p = by["duplicate_load"]
    con.execute(f"INSERT INTO raw_shipments SELECT * FROM raw_shipments WHERE load_date IN ({dates(p)})")
    p = by["cents_shift"]
    con.execute(f"UPDATE raw_payments SET amount = round(amount * 100, 0) WHERE load_date IN ({dates(p)})")
    p = by["null_spike"]
    con.execute(f"UPDATE raw_customers SET email = NULL WHERE load_date IN ({dates(p)}) "
                f"AND {U('nul', 'customer_id')} < 0.35")
    p = by["orphan_returns"]
    con.execute(f"UPDATE raw_returns SET order_id = 900000000000 + return_id WHERE load_date IN ({dates(p)}) "
                f"AND {U('orph', 'return_id')} < 0.4")
    p = by["silent_quantity_bug"]
    con.execute(f"UPDATE raw_order_items SET quantity = 1 WHERE load_date IN ({dates(p)})")


def build(cfg: Config, db_path: Path | str, scale: float = 1.0, seed: int = SEED) -> dict:
    """Create the warehouse at db_path, replacing any existing file. Returns build stats."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", ".wal"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    rng = random.Random(seed)
    counts = counts_frame(cfg, scale, rng)
    con = duckdb.connect(str(db_path))
    try:
        rows = []
        cum = 0
        for d in range(cfg.days):
            rows.append((d, cfg.day_date(d), counts["customers"][d], cum, counts["orders"][d], counts["items"][d],
                         counts["web"][d], counts["support"][d], counts["products"][d], counts["products"][d] * 3,
                         40, 4))
            cum += counts["customers"][d]
        con.execute("""CREATE TEMP TABLE _counts (d INTEGER, load_date DATE, n_customers INTEGER, cum_customers BIGINT,
            n_orders INTEGER, n_items INTEGER, n_web INTEGER, n_support INTEGER, n_products INTEGER,
            n_inventory INTEGER, n_campaigns INTEGER, n_fx INTEGER)""")
        con.executemany("INSERT INTO _counts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        order = ["raw_fx_rates", "raw_products", "raw_customers", "raw_orders", "raw_order_items", "raw_payments",
                 "raw_refunds", "raw_shipments", "raw_returns", "raw_inventory", "raw_web_sessions",
                 "raw_marketing_spend", "raw_support_tickets"]
        sqls = raw_sql()
        for name in order:
            cols = ", ".join(cfg.tables[name].columns)
            con.execute(f"CREATE TABLE {name} AS SELECT {cols}, load_date FROM ({sqls[name]}) ORDER BY load_date")
        apply_data_incidents(con, cfg)
        for name in cfg.topo_order():
            if name in TRANSFORMS:
                cols = ", ".join(cfg.tables[name].columns)
                con.execute(f"CREATE TABLE {name} AS SELECT {cols}, load_date FROM ({TRANSFORMS[name]}) "
                            "ORDER BY load_date")
        _write_load_log(con, cfg, rng)
        _write_schema_log(con, cfg)
        # The planted schema change, applied physically so the live schema matches the latest snapshot.
        con.execute("ALTER TABLE raw_customers ALTER postal_code TYPE VARCHAR")
        planted = []
        for p in PLANTED:
            p = PlantedIncident(**{**p.to_dict(), "affected": [p.table, *sorted(cfg.descendants(p.table))]})
            planted.append(p)
        con.execute("CREATE TABLE _dw_planted (id VARCHAR, kind VARCHAR, table_name VARCHAR, column_name VARCHAR, "
                    "start_day INTEGER, end_day INTEGER, monitors VARCHAR, description VARCHAR, affected VARCHAR)")
        con.executemany("INSERT INTO _dw_planted VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [(p.id, p.kind, p.table, p.column, p.start_day, p.end_day, ",".join(p.monitors),
                          p.description, ",".join(p.affected)) for p in planted])
        con.execute("CREATE TABLE _dw_meta AS SELECT ? AS scale, ? AS seed, ? AS days, now() AS built_at",
                    [scale, seed, cfg.days])
        total = sum(con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in cfg.tables)
    finally:
        con.close()
    return {"tables": len(cfg.tables), "rows": total, "days": cfg.days, "scale": scale, "path": str(db_path)}


def _write_load_log(con: duckdb.DuckDBPyConnection, cfg: Config, rng: random.Random) -> None:
    """When each partition landed. Sources arrive near their usual hour; models run after all their inputs."""
    late = next(p for p in PLANTED if p.id == "late_load")
    arrival: dict[str, list[float]] = {}
    for name in cfg.topo_order():
        t = cfg.tables[name]
        hrs = []
        for d in range(cfg.days):
            if not t.upstream:
                h = max(0.2, ARRIVAL_HOURS[name] + rng.gauss(0, 0.2))
                if name == late.table and late.start_day <= d <= late.end_day:
                    h += 10.0
            else:
                h = max(arrival[u][d] for u in t.upstream) + 0.1 + rng.random() * 0.3
            hrs.append(round(h, 3))
        arrival[name] = hrs
    counts = {
        name: dict(con.execute(f"SELECT load_date, count(*) FROM {name} GROUP BY 1").fetchall()) for name in cfg.tables
    }
    rows = []
    for name, hrs in arrival.items():
        for d, h in enumerate(hrs):
            ld = cfg.day_date(d)
            loaded_at = datetime.combine(ld + timedelta(days=1), datetime.min.time()) + timedelta(hours=h)
            rows.append((name, d, ld, loaded_at, h, counts[name].get(ld, 0)))
    con.execute("CREATE TABLE _dw_load_log (table_name VARCHAR, day INTEGER, load_date DATE, loaded_at TIMESTAMP, "
                "arrival_hours DOUBLE, row_count BIGINT)")
    values = ", ".join(f"('{n}', {d}, DATE '{ld}', TIMESTAMP '{ts}', {h}, {c})" for n, d, ld, ts, h, c in rows)
    con.execute(f"INSERT INTO _dw_load_log VALUES {values}")


def _write_schema_log(con: duckdb.DuckDBPyConnection, cfg: Config) -> None:
    """Schema snapshot captured by the loader after each daily load (information_schema at that time)."""
    change = next(p for p in PLANTED if p.id == "schema_change")
    names = ", ".join(f"'{n}'" for n in cfg.tables)
    con.execute(f"""CREATE TABLE _dw_schema_log AS
        SELECT c.table_name, d.day::INTEGER AS day, c.column_name,
               CASE WHEN c.table_name = '{change.table}' AND c.column_name = '{change.column}'
                         AND d.day >= {change.start_day} THEN 'VARCHAR' ELSE c.data_type END AS data_type,
               c.ordinal_position AS ordinal
        FROM information_schema.columns c, range({cfg.days}) d(day)
        WHERE c.table_name IN ({names}) AND c.table_schema = 'main'
        ORDER BY 1, 2, 5""")


def load_planted(con: duckdb.DuckDBPyConnection) -> list[PlantedIncident]:
    out = []
    for r in con.execute("SELECT * FROM _dw_planted ORDER BY start_day").fetchall():
        out.append(PlantedIncident(r[0], r[1], r[2], r[3], r[4], r[5], r[6].split(","), r[7], r[8].split(",")))
    return out
