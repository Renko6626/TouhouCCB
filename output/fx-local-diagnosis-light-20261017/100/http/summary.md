# FX load-test report

Target: http://127.0.0.1:18543
Abort: 30s window: successful trade P99 exceeds 2000ms

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.727 | 3.727 |
| readonly | 0 | 0.0 | 7.089 |
| normal | 70 | 3.707 | 25.236 |
| active | 90 | 7.148 | 43.162 |
| stress | 100 | 8.412 | 55.252 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| active / cash_buy | {'ok': 53} | 1363.114 | 1378.783 |
| active / cash_sell | {'ok': 46} | 1079.195 | 1298.165 |
| active / cross_zero | {'ok': 3} | 1076.821 | 1076.821 |
| active / financed_buy | {'ok': 12} | 1576.473 | 1576.473 |
| active / sell_repay | {'ok': 14} | 2245.97 | 2245.97 |
| active / short_cover | {'ok': 14} | 1000.076 | 1000.076 |
| active / short_open | {'ok': 13} | 2382.419 | 2382.419 |
| normal / cash_buy | {'ok': 46} | 318.44 | 374.308 |
| normal / cash_sell | {'ok': 6} | 56.279 | 56.279 |
| normal / cross_zero | {'ok': 1} | 58.745 | 58.745 |
| normal / financed_buy | {'ok': 13} | 759.701 | 759.701 |
| normal / sell_repay | {'ok': 1} | 81.847 | 81.847 |
| normal / short_cover | {'ok': 1} | 66.547 | 66.547 |
| normal / short_open | {'ok': 10} | 673.126 | 673.126 |
| role_prepare / cash_buy | {'ok': 5} | 25.206 | 25.206 |
| role_prepare / cross_zero | {'ok': 5} | 74.212 | 74.212 |
| role_prepare / financed_buy | {'ok': 20} | 52.962 | 85.15 |
| role_prepare / sell_repay | {'ok': 20} | 45.92 | 46.374 |
| role_prepare / short_cover | {'ok': 20} | 33.122 | 34.411 |
| role_prepare / short_open | {'ok': 15} | 52.552 | 52.552 |
| smoke / cash_buy | {'ok': 4} | 89.687 | 89.687 |
| smoke / cash_sell | {'ok': 4} | 41.29 | 41.29 |
| stress / cash_buy | {'ok': 124} | 8825.603 | 8938.357 |
| stress / cash_sell | {'ok': 121} | 8764.249 | 9015.93 |
| stress / cross_zero | {'timeout': 4, 'version_conflict': 4, 'ok': 1} | 8970.769 | 8970.769 |
| stress / financed_buy | {'ok': 14, 'timeout': 8, 'version_conflict': 3} | 14845.292 | 14845.292 |
| stress / sell_repay | {'ok': 11, 'timeout': 10, 'version_conflict': 9} | 14519.869 | 14519.869 |
| stress / short_cover | {'ok': 23} | 7796.635 | 8438.155 |
| stress / short_open | {'ok': 5, 'timeout': 13, 'version_conflict': 7} | 13918.443 | 13918.443 |

Coverage: `{"chart_sse_tail_reused": 735, "cash_buy": 232, "cash_sell": 177, "financed_buy": 59, "sell_repay": 46, "cross_zero": 10, "short_cover": 58, "short_open": 43}`

See summary.json and requests.jsonl for errors and sampling boundaries.
