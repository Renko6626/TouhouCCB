# FX load-test report

Target: http://127.0.0.1:18543
Abort: 30s window: successful trade P99 exceeds 2000ms

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.727 | 3.727 |
| readonly | 0 | 0.0 | 6.595 |
| normal | 70 | 2.485 | 19.142 |
| active | 90 | 7.326 | 44.382 |
| stress | 100 | 9.379 | 59.277 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| active / cash_buy | {'ok': 143} | 621.223 | 800.713 |
| active / cash_sell | {'ok': 136} | 659.589 | 871.119 |
| active / cross_zero | {'ok': 8} | 1209.803 | 1209.803 |
| active / financed_buy | {'ok': 41} | 1220.407 | 1500.962 |
| active / sell_repay | {'ok': 47} | 1202.105 | 1567.145 |
| active / short_cover | {'ok': 39} | 757.477 | 921.613 |
| active / short_open | {'ok': 33} | 1266.516 | 1371.497 |
| normal / cash_buy | {'ok': 255} | 115.784 | 209.852 |
| normal / cash_sell | {'ok': 210} | 121.715 | 218.489 |
| normal / cross_zero | {'ok': 13} | 217.402 | 217.402 |
| normal / financed_buy | {'ok': 82} | 135.279 | 169.709 |
| normal / sell_repay | {'ok': 69} | 148.395 | 498.889 |
| normal / short_cover | {'ok': 62} | 114.202 | 325.774 |
| normal / short_open | {'ok': 57} | 126.724 | 212.899 |
| role_prepare / cash_buy | {'ok': 5} | 23.367 | 23.367 |
| role_prepare / cross_zero | {'ok': 5} | 46.885 | 46.885 |
| role_prepare / financed_buy | {'ok': 20} | 53.185 | 67.474 |
| role_prepare / sell_repay | {'ok': 20} | 38.491 | 49.153 |
| role_prepare / short_cover | {'ok': 20} | 29.356 | 47.059 |
| role_prepare / short_open | {'ok': 15} | 41.679 | 41.679 |
| smoke / cash_buy | {'ok': 4} | 94.288 | 94.288 |
| smoke / cash_sell | {'ok': 4} | 57.007 | 57.007 |
| stress / cash_buy | {'ok': 131} | 9747.676 | 9809.983 |
| stress / cash_sell | {'ok': 126} | 9583.616 | 9622.028 |
| stress / cross_zero | {'ok': 4, 'timeout': 2, 'version_conflict': 1} | 14842.15 | 14842.15 |
| stress / financed_buy | {'ok': 18, 'timeout': 6, 'version_conflict': 6} | 14387.133 | 14387.133 |
| stress / sell_repay | {'ok': 15, 'timeout': 7, 'version_conflict': 5} | 14857.393 | 14857.393 |
| stress / short_cover | {'ok': 21} | 9160.506 | 9582.287 |
| stress / short_open | {'ok': 7, 'timeout': 12, 'version_conflict': 10} | 14514.663 | 14514.663 |

Coverage: `{"chart_sse_tail_reused": 1720, "cash_buy": 538, "cash_sell": 476, "financed_buy": 161, "sell_repay": 151, "cross_zero": 30, "short_cover": 142, "short_open": 112}`

See summary.json and requests.jsonl for errors and sampling boundaries.
