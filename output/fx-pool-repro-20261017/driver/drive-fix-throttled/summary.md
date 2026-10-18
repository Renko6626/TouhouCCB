# FX load-test report

Target: http://127.0.0.1:55571
Abort: 30s window: successful trade P99 exceeds 2000ms

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.143 | 0.762 |
| readonly | 0 | 0.0 | 6.609 |
| normal | 54 | 2.059 | 19.162 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| normal / cash_buy | {'ok': 23} | 3557.4 | 4520.889 |
| normal / cash_sell | {'ok': 26} | 4684.508 | 4936.347 |
| normal / cross_zero | {} | — | — |
| normal / financed_buy | {'ok': 10} | 6356.815 | 6356.815 |
| normal / sell_repay | {'ok': 3} | 8190.28 | 8190.28 |
| normal / short_cover | {'ok': 2} | 3465.839 | 3465.839 |
| normal / short_open | {'ok': 5} | 5674.245 | 5674.245 |
| role_prepare / cash_buy | {'ok': 5} | 166.074 | 166.074 |
| role_prepare / cash_sell | {'ok': 1} | 61.525 | 61.525 |
| role_prepare / cross_zero | {'ok': 5} | 230.284 | 230.284 |
| role_prepare / financed_buy | {'ok': 20} | 255.213 | 790.309 |
| role_prepare / sell_repay | {'ok': 25} | 223.776 | 235.693 |
| role_prepare / short_cover | {'ok': 24} | 186.293 | 746.928 |
| role_prepare / short_open | {'ok': 15} | 208.747 | 208.747 |
| smoke / cash_buy | {'ok': 2} | 118.409 | 118.409 |
| smoke / cash_sell | {'ok': 1} | 81.403 | 81.403 |

Coverage: `{"chart_sse_tail_reused": 267, "cash_buy": 30, "cash_sell": 28, "financed_buy": 30, "sell_repay": 28, "cross_zero": 5, "short_cover": 26, "short_open": 20}`

See summary.json and requests.jsonl for errors and sampling boundaries.
