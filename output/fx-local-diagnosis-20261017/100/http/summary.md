# FX load-test report

Target: http://127.0.0.1:18543
Abort: 30s window: successful trade P99 exceeds 2000ms

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.636 | 3.181 |
| readonly | 0 | 0.0 | 6.362 |
| normal | 70 | 2.864 | 21.223 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| normal / cash_buy | {'ok': 46} | 1003.932 | 1064.915 |
| normal / cash_sell | {'ok': 16} | 520.016 | 520.016 |
| normal / cross_zero | {'ok': 2} | 203.756 | 203.756 |
| normal / financed_buy | {'ok': 14} | 2023.752 | 2023.752 |
| normal / sell_repay | {'ok': 2} | 120.226 | 120.226 |
| normal / short_cover | {'ok': 2} | 332.642 | 332.642 |
| normal / short_open | {'ok': 8} | 1968.883 | 1968.883 |
| role_prepare / cash_buy | {'ok': 5} | 45.747 | 45.747 |
| role_prepare / cross_zero | {'ok': 5} | 107.409 | 107.409 |
| role_prepare / financed_buy | {'ok': 20} | 89.813 | 127.164 |
| role_prepare / sell_repay | {'ok': 20} | 89.754 | 163.155 |
| role_prepare / short_cover | {'ok': 20} | 61.406 | 62.19 |
| role_prepare / short_open | {'ok': 15} | 80.42 | 80.42 |
| smoke / cash_buy | {'ok': 4} | 146.841 | 146.841 |
| smoke / cash_sell | {'ok': 3} | 96.797 | 96.797 |

Coverage: `{"chart_sse_tail_reused": 282, "cash_buy": 55, "cash_sell": 19, "financed_buy": 34, "sell_repay": 22, "cross_zero": 7, "short_cover": 22, "short_open": 23}`

See summary.json and requests.jsonl for errors and sampling boundaries.
