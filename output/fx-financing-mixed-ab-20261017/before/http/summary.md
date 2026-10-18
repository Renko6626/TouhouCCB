# FX load-test report

Target: http://127.0.0.1:18543
Abort: 30s window: successful trade P99 exceeds 2000ms

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.727 | 3.727 |
| readonly | 0 | 0.0 | 6.451 |
| normal | 70 | 2.522 | 19.292 |
| active | 90 | 7.235 | 44.551 |
| stress | 100 | 9.721 | 60.206 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| active / cash_buy | {'ok': 109} | 892.95 | 1026.916 |
| active / cash_sell | {'ok': 104} | 573.618 | 889.415 |
| active / cross_zero | {'ok': 7} | 1916.687 | 1916.687 |
| active / financed_buy | {'ok': 28} | 1503.757 | 1822.674 |
| active / sell_repay | {'ok': 34} | 1000.123 | 1120.323 |
| active / short_cover | {'ok': 29} | 785.434 | 886.35 |
| active / short_open | {'ok': 25} | 1260.244 | 1608.91 |
| normal / cash_buy | {'ok': 68} | 206.029 | 235.366 |
| normal / cash_sell | {'ok': 31} | 97.128 | 110.705 |
| normal / cross_zero | {'ok': 2} | 89.379 | 89.379 |
| normal / financed_buy | {'ok': 21} | 210.75 | 391.124 |
| normal / sell_repay | {'ok': 8} | 182.919 | 182.919 |
| normal / short_cover | {'ok': 10} | 110.823 | 110.823 |
| normal / short_open | {'ok': 14} | 287.884 | 287.884 |
| role_prepare / cash_buy | {'ok': 5} | 24.212 | 24.212 |
| role_prepare / cross_zero | {'ok': 5} | 63.612 | 63.612 |
| role_prepare / financed_buy | {'ok': 20} | 57.369 | 202.833 |
| role_prepare / sell_repay | {'ok': 20} | 47.266 | 66.318 |
| role_prepare / short_cover | {'ok': 20} | 29.228 | 51.145 |
| role_prepare / short_open | {'ok': 15} | 53.3 | 53.3 |
| smoke / cash_buy | {'ok': 4} | 55.709 | 55.709 |
| smoke / cash_sell | {'ok': 4} | 53.142 | 53.142 |
| stress / cash_buy | {'ok': 142} | 8405.958 | 8565.769 |
| stress / cash_sell | {'ok': 131} | 8499.621 | 8606.746 |
| stress / cross_zero | {'ok': 5, 'timeout': 2, 'version_conflict': 2} | 14409.706 | 14409.706 |
| stress / financed_buy | {'ok': 15, 'timeout': 6, 'version_conflict': 5} | 13951.732 | 13951.732 |
| stress / sell_repay | {'ok': 12, 'timeout': 8, 'version_conflict': 6} | 14532.298 | 14532.298 |
| stress / short_cover | {'ok': 22} | 8246.887 | 8298.418 |
| stress / short_open | {'ok': 9, 'timeout': 11, 'version_conflict': 9} | 14785.23 | 14785.23 |

Coverage: `{"chart_sse_tail_reused": 1029, "cash_buy": 328, "cash_sell": 270, "financed_buy": 84, "sell_repay": 74, "cross_zero": 19, "short_cover": 81, "short_open": 63}`

See summary.json and requests.jsonl for errors and sampling boundaries.
