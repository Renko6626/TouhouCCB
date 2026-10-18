# FX load-test report

Target: http://127.0.0.1:18543
Abort: 30s window: successful trade P99 exceeds 2000ms

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.727 | 3.727 |
| readonly | 0 | 0.0 | 6.451 |
| normal | 70 | 2.475 | 19.065 |
| active | 90 | 7.279 | 44.168 |
| stress | 100 | 9.561 | 60.201 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| active / cash_buy | {'ok': 106} | 662.438 | 783.892 |
| active / cash_sell | {'ok': 105} | 363.952 | 592.386 |
| active / cross_zero | {'ok': 8} | 967.788 | 967.788 |
| active / financed_buy | {'ok': 33} | 624.532 | 681.3 |
| active / sell_repay | {'ok': 34} | 1021.732 | 1097.386 |
| active / short_cover | {'ok': 31} | 628.255 | 630.254 |
| active / short_open | {'ok': 20} | 856.464 | 972.389 |
| normal / cash_buy | {'ok': 69} | 122.335 | 230.179 |
| normal / cash_sell | {'ok': 29} | 110.714 | 278.394 |
| normal / cross_zero | {'ok': 2} | 83.833 | 83.833 |
| normal / financed_buy | {'ok': 18} | 199.881 | 199.881 |
| normal / sell_repay | {'ok': 8} | 114.766 | 114.766 |
| normal / short_cover | {'ok': 9} | 62.07 | 62.07 |
| normal / short_open | {'ok': 16} | 288.877 | 288.877 |
| role_prepare / cash_buy | {'ok': 5} | 29.941 | 29.941 |
| role_prepare / cross_zero | {'ok': 5} | 52.69 | 52.69 |
| role_prepare / financed_buy | {'ok': 20} | 53.72 | 79.384 |
| role_prepare / sell_repay | {'ok': 20} | 44.727 | 46.49 |
| role_prepare / short_cover | {'ok': 20} | 30.869 | 34.517 |
| role_prepare / short_open | {'ok': 15} | 45.909 | 45.909 |
| smoke / cash_buy | {'ok': 4} | 53.013 | 53.013 |
| smoke / cash_sell | {'ok': 4} | 53.78 | 53.78 |
| stress / cash_buy | {'ok': 129} | 9136.728 | 9285.843 |
| stress / cash_sell | {'ok': 127} | 9203.502 | 9262.785 |
| stress / cross_zero | {'timeout': 4, 'version_conflict': 3, 'ok': 1} | 13265.699 | 13265.699 |
| stress / financed_buy | {'ok': 23} | 9032.296 | 9070.348 |
| stress / sell_repay | {'ok': 13, 'timeout': 16, 'version_conflict': 10} | 12973.772 | 12973.772 |
| stress / short_cover | {'ok': 18} | 9270.821 | 9270.821 |
| stress / short_open | {'ok': 16, 'timeout': 8, 'version_conflict': 6} | 14671.861 | 14671.861 |

Coverage: `{"chart_sse_tail_reused": 1018, "cash_buy": 313, "cash_sell": 265, "financed_buy": 94, "sell_repay": 75, "cross_zero": 16, "short_cover": 78, "short_open": 67}`

See summary.json and requests.jsonl for errors and sampling boundaries.
