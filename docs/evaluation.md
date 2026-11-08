| Incident type | Source table | Learned: detected | Learned: time to detect | Static: detected | Static: time to detect | Root cause correct | Tables flagged / downstream reach |
|---|---|---|---|---|---|---|---|
| volume drop | `raw_web_sessions` | yes | 0.0 h | yes | 0.0 h | yes | 5 / 5 |
| freshness | `raw_inventory` | yes | 1.1 h | yes | 5.0 h | yes | 3 / 3 |
| schema | `raw_customers` | yes | 0.0 h | yes | 0.0 h | yes | 1 / 4 |
| volume spike | `raw_shipments` | yes | 0.0 h | yes | 0.0 h | yes | 1 / 4 |
| distribution shift | `raw_payments` | yes | 0.0 h | yes | 0.0 h | yes | 5 / 6 |
| null rate | `raw_customers` | yes | 0.0 h | yes | 0.0 h | yes | 3 / 4 |
| referential | `raw_returns` | yes | 0.0 h | yes | 0.0 h | yes | 2 / 5 |
| silent upstream bug | `raw_order_items` | yes | 0.0 h | yes | 0.0 h | yes | 7 / 7 |

| Metric | Learned baselines | Static thresholds |
|---|---|---|
| Detection rate | 100% | 100% |
| Median time to detection | 0.0 h | 0.0 h |
| Alerts (table, monitor, day) | 89 | 1150 |
| False alerts | 23 | 1025 |
| False alerts per table per week | 0.043 | 1.902 |
| Incidents after grouping | 18 | 7 |
| Incidents with no planted cause | 10 | 5 |
| Root cause correct | 100% | 12% |
| Alert volume removed by grouping | 80% | 99% |
| Pages, tickets, and updates sent | 26 | 222 |
