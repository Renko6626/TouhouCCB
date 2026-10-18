# FX load-test report

Target: http://127.0.0.1:18543
Abort: 30s window: successful trade P99 exceeds 2000ms

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.727 | 3.727 |
| readonly | 0 | 0.0 | 6.451 |
| normal | 70 | 2.505 | 26.014 |
| active | 90 | 7.336 | 57.958 |
| stress | 100 | 9.772 | 71.73 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| active / cash_buy | {'ok': 109} | 477.795 | 628.04 |
| active / cash_sell | {'ok': 109} | 421.908 | 500.278 |
| active / cross_zero | {'ok': 8} | 498.527 | 498.527 |
| active / financed_buy | {'ok': 33} | 597.135 | 612.896 |
| active / sell_repay | {'ok': 32} | 766.775 | 942.681 |
| active / short_cover | {'ok': 27} | 504.141 | 628.46 |
| active / short_open | {'ok': 24} | 879.755 | 883.913 |
| normal / cash_buy | {'ok': 68} | 105.072 | 152.401 |
| normal / cash_sell | {'ok': 31} | 100.607 | 110.16 |
| normal / cross_zero | {'ok': 2} | 53.284 | 53.284 |
| normal / financed_buy | {'ok': 19} | 230.756 | 230.756 |
| normal / sell_repay | {'ok': 7} | 188.045 | 188.045 |
| normal / short_cover | {'ok': 10} | 77.924 | 77.924 |
| normal / short_open | {'ok': 16} | 199.158 | 199.158 |
| role_prepare / cash_buy | {'ok': 5} | 35.406 | 35.406 |
| role_prepare / cross_zero | {'ok': 5} | 57.157 | 57.157 |
| role_prepare / financed_buy | {'ok': 20} | 48.761 | 79.193 |
| role_prepare / sell_repay | {'ok': 20} | 47.5 | 50.102 |
| role_prepare / short_cover | {'ok': 20} | 29.442 | 29.577 |
| role_prepare / short_open | {'ok': 15} | 190.51 | 190.51 |
| smoke / cash_buy | {'ok': 4} | 79.873 | 79.873 |
| smoke / cash_sell | {'ok': 4} | 42.845 | 42.845 |
| stress / cash_buy | {'ok': 129} | 8517.508 | 8732.899 |
| stress / cash_sell | {'ok': 130} | 8556.095 | 8592.311 |
| stress / cross_zero | {'ok': 3, 'timeout': 3, 'version_conflict': 2} | 13009.834 | 13009.834 |
| stress / financed_buy | {'ok': 20} | 8180.041 | 8560.496 |
| stress / sell_repay | {'ok': 20, 'timeout': 12, 'version_conflict': 11, 'business_rejected': 1} | 14473.511 | 14479.759 |
| stress / short_cover | {'ok': 21} | 8468.555 | 8558.342 |
| stress / short_open | {'ok': 12, 'timeout': 9, 'version_conflict': 8} | 14912.335 | 14912.335 |

Coverage: `{"chart_sse_tail_reused": 366, "cash_buy": 315, "cash_sell": 274, "financed_buy": 92, "sell_repay": 79, "cross_zero": 18, "short_cover": 78, "short_open": 67}`

See summary.json and requests.jsonl for errors and sampling boundaries.
