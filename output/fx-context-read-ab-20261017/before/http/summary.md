# FX load-test report

Target: http://127.0.0.1:18543
Abort: 30s window: successful trade P99 exceeds 2000ms

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.727 | 3.726 |
| readonly | 0 | 0.0 | 6.451 |
| normal | 70 | 2.508 | 19.162 |
| active | 90 | 7.187 | 43.511 |
| stress | 100 | 9.437 | 57.932 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| active / cash_buy | {'ok': 106} | 534.341 | 715.502 |
| active / cash_sell | {'ok': 101} | 611.192 | 730.763 |
| active / cross_zero | {'ok': 7} | 1241.967 | 1241.967 |
| active / financed_buy | {'ok': 32} | 1027.954 | 1302.5 |
| active / sell_repay | {'ok': 35} | 1247.173 | 1280.151 |
| active / short_cover | {'ok': 32} | 649.772 | 832.39 |
| active / short_open | {'ok': 21} | 935.191 | 1217.378 |
| normal / cash_buy | {'ok': 69} | 124.229 | 173.793 |
| normal / cash_sell | {'ok': 32} | 175.013 | 259.742 |
| normal / cross_zero | {'ok': 2} | 130.272 | 130.272 |
| normal / financed_buy | {'ok': 20} | 377.38 | 395.268 |
| normal / sell_repay | {'ok': 8} | 139.729 | 139.729 |
| normal / short_cover | {'ok': 7} | 121.195 | 121.195 |
| normal / short_open | {'ok': 15} | 300.541 | 300.541 |
| role_prepare / cash_buy | {'ok': 5} | 26.867 | 26.867 |
| role_prepare / cross_zero | {'ok': 5} | 66.78 | 66.78 |
| role_prepare / financed_buy | {'ok': 20} | 64.624 | 65.864 |
| role_prepare / sell_repay | {'ok': 20} | 47.897 | 48.964 |
| role_prepare / short_cover | {'ok': 20} | 35.28 | 39.365 |
| role_prepare / short_open | {'ok': 15} | 54.715 | 54.715 |
| smoke / cash_buy | {'ok': 4} | 79.485 | 79.485 |
| smoke / cash_sell | {'ok': 4} | 48.104 | 48.104 |
| stress / cash_buy | {'ok': 133} | 9070.413 | 9135.963 |
| stress / cash_sell | {'ok': 126} | 8906.475 | 9145.286 |
| stress / cross_zero | {'ok': 5, 'timeout': 1} | 14641.81 | 14641.81 |
| stress / financed_buy | {'ok': 15, 'timeout': 8, 'version_conflict': 7} | 14573.324 | 14573.324 |
| stress / sell_repay | {'ok': 12, 'timeout': 8, 'version_conflict': 8} | 14997.602 | 14997.602 |
| stress / short_cover | {'ok': 21} | 8752.406 | 8794.876 |
| stress / short_open | {'ok': 20, 'timeout': 6, 'version_conflict': 5} | 13898.212 | 14764.526 |

Coverage: `{"chart_sse_tail_reused": 1022, "cash_buy": 317, "cash_sell": 263, "financed_buy": 87, "sell_repay": 75, "cross_zero": 19, "short_cover": 80, "short_open": 71}`

See summary.json and requests.jsonl for errors and sampling boundaries.
