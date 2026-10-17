# FX load-test report

Target: http://127.0.0.1:18543
Abort: 30s window: successful trade P99 exceeds 2000ms

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.727 | 3.726 |
| readonly | 0 | 0.0 | 6.451 |
| normal | 70 | 2.459 | 18.901 |
| active | 90 | 7.301 | 44.022 |
| stress | 100 | 10.59 | 64.331 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| active / cash_buy | {'ok': 107} | 322.898 | 450.187 |
| active / cash_sell | {'ok': 106} | 422.904 | 479.261 |
| active / cross_zero | {'ok': 6} | 583.834 | 583.834 |
| active / financed_buy | {'ok': 33} | 401.544 | 406.862 |
| active / sell_repay | {'ok': 35} | 573.882 | 703.192 |
| active / short_cover | {'ok': 27} | 286.263 | 318.628 |
| active / short_open | {'ok': 23} | 723.21 | 822.812 |
| normal / cash_buy | {'ok': 66} | 137.458 | 206.793 |
| normal / cash_sell | {'ok': 30} | 212.687 | 222.502 |
| normal / cross_zero | {'ok': 2} | 84.497 | 84.497 |
| normal / financed_buy | {'ok': 19} | 160.944 | 160.944 |
| normal / sell_repay | {'ok': 8} | 109.267 | 109.267 |
| normal / short_cover | {'ok': 9} | 91.993 | 91.993 |
| normal / short_open | {'ok': 16} | 171.659 | 171.659 |
| role_prepare / cash_buy | {'ok': 5} | 25.468 | 25.468 |
| role_prepare / cross_zero | {'ok': 5} | 61.745 | 61.745 |
| role_prepare / financed_buy | {'ok': 20} | 47.054 | 83.306 |
| role_prepare / sell_repay | {'ok': 20} | 42.716 | 44.558 |
| role_prepare / short_cover | {'ok': 20} | 28.007 | 29.514 |
| role_prepare / short_open | {'ok': 15} | 44.24 | 44.24 |
| smoke / cash_buy | {'ok': 4} | 82.447 | 82.447 |
| smoke / cash_sell | {'ok': 4} | 41.471 | 41.471 |
| stress / cash_buy | {'ok': 140} | 8376.425 | 8604.959 |
| stress / cash_sell | {'ok': 130} | 8543.863 | 8699.874 |
| stress / cross_zero | {'ok': 5, 'timeout': 2, 'version_conflict': 2} | 13305.115 | 13305.115 |
| stress / financed_buy | {'ok': 24} | 8359.726 | 8742.379 |
| stress / sell_repay | {'ok': 20, 'timeout': 12, 'version_conflict': 12} | 14189.68 | 14936.538 |
| stress / short_cover | {'ok': 25} | 8433.873 | 8490.475 |
| stress / short_open | {'ok': 17, 'timeout': 7, 'version_conflict': 4} | 14400.789 | 14400.789 |

Coverage: `{"chart_sse_tail_reused": 1051, "cash_buy": 322, "cash_sell": 270, "financed_buy": 96, "sell_repay": 83, "cross_zero": 18, "short_cover": 81, "short_open": 71}`

See summary.json and requests.jsonl for errors and sampling boundaries.
