# FX load-test report

Target: http://127.0.0.1:55571
Abort: 30s window: successful trade P99 exceeds 2000ms

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.143 | 0.762 |
| readonly | 0 | 0.0 | 6.487 |
| normal | 70 | 2.526 | 19.444 |
| active | 90 | 6.99 | 42.845 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| active / cash_buy | {'ok': 88} | 1931.288 | 2365.187 |
| active / cash_sell | {'ok': 57} | 2034.684 | 2394.005 |
| active / cross_zero | {'ok': 4} | 3344.537 | 3344.537 |
| active / financed_buy | {'ok': 25} | 3510.152 | 3824.298 |
| active / sell_repay | {'ok': 18} | 3954.517 | 3954.517 |
| active / short_cover | {'ok': 18} | 2228.89 | 2228.89 |
| active / short_open | {'ok': 14} | 3962.436 | 3962.436 |
| normal / cash_buy | {'ok': 107} | 168.106 | 187.014 |
| normal / cash_sell | {'ok': 89} | 150.849 | 335.132 |
| normal / cross_zero | {'ok': 6} | 153.295 | 153.295 |
| normal / financed_buy | {'ok': 32} | 345.521 | 378.446 |
| normal / sell_repay | {'ok': 27} | 239.953 | 241.772 |
| normal / short_cover | {'ok': 23} | 218.618 | 234.992 |
| normal / short_open | {'ok': 22} | 153.93 | 363.679 |
| role_prepare / cash_buy | {'ok': 5} | 25.52 | 25.52 |
| role_prepare / cross_zero | {'ok': 5} | 55.497 | 55.497 |
| role_prepare / financed_buy | {'ok': 20} | 76.396 | 87.342 |
| role_prepare / sell_repay | {'ok': 20} | 54.345 | 54.975 |
| role_prepare / short_cover | {'ok': 20} | 43.357 | 48.404 |
| role_prepare / short_open | {'ok': 15} | 100.764 | 100.764 |
| smoke / cash_buy | {'ok': 2} | 63.575 | 63.575 |
| smoke / cash_sell | {'ok': 1} | 35.369 | 35.369 |

Coverage: `{"chart_sse_tail_reused": 728, "cash_buy": 202, "cash_sell": 147, "financed_buy": 77, "sell_repay": 65, "cross_zero": 15, "short_cover": 61, "short_open": 51}`

See summary.json and requests.jsonl for errors and sampling boundaries.
