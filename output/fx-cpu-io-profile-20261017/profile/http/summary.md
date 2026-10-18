# FX load-test report

Target: http://127.0.0.1:18543
Abort: 30s window: successful trade P99 exceeds 2000ms

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.727 | 3.727 |
| readonly | 0 | 0.0 | 6.451 |
| normal | 70 | 2.44 | 18.816 |
| active | 90 | 7.307 | 45.456 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| active / cash_buy | {'ok': 75} | 1465.438 | 1686.052 |
| active / cash_sell | {'ok': 74} | 1370.792 | 1709.362 |
| active / cross_zero | {'ok': 5} | 1565.824 | 1565.824 |
| active / financed_buy | {'ok': 19} | 1667.711 | 1667.711 |
| active / sell_repay | {'ok': 25} | 2932.687 | 3240.977 |
| active / short_cover | {'ok': 22} | 1089.274 | 1104.317 |
| active / short_open | {'ok': 15} | 2394.286 | 2394.286 |
| normal / cash_buy | {'ok': 66} | 115.039 | 147.193 |
| normal / cash_sell | {'ok': 30} | 120.043 | 146.35 |
| normal / cross_zero | {'ok': 2} | 94.418 | 94.418 |
| normal / financed_buy | {'ok': 19} | 517.555 | 517.555 |
| normal / sell_repay | {'ok': 8} | 157.346 | 157.346 |
| normal / short_cover | {'ok': 9} | 79.005 | 79.005 |
| normal / short_open | {'ok': 15} | 192.767 | 192.767 |
| role_prepare / cash_buy | {'ok': 5} | 33.561 | 33.561 |
| role_prepare / cross_zero | {'ok': 5} | 53.194 | 53.194 |
| role_prepare / financed_buy | {'ok': 20} | 51.506 | 65.266 |
| role_prepare / sell_repay | {'ok': 20} | 59.587 | 61.399 |
| role_prepare / short_cover | {'ok': 20} | 32.577 | 32.581 |
| role_prepare / short_open | {'ok': 15} | 57.489 | 57.489 |
| smoke / cash_buy | {'ok': 4} | 94.62 | 94.62 |
| smoke / cash_sell | {'ok': 4} | 55.325 | 55.325 |

Coverage: `{"chart_sse_tail_reused": 587, "cash_buy": 150, "cash_sell": 108, "financed_buy": 58, "sell_repay": 53, "cross_zero": 12, "short_cover": 51, "short_open": 45}`

See summary.json and requests.jsonl for errors and sampling boundaries.
