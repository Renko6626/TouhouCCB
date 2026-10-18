# FX load-test report

Target: http://127.0.0.1:18543
Abort: 30s window: successful trade P99 exceeds 2000ms

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.727 | 3.727 |
| readheavy | 30 | 0.499 | 22.473 |
| readonly | 0 | 0.0 | 6.451 |
| normal | 70 | 2.536 | 19.225 |
| active | 90 | 7.223 | 42.6 |
| stress | 100 | 8.895 | 50.782 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| active / cash_buy | {'ok': 112} | 606.937 | 740.601 |
| active / cash_sell | {'ok': 98} | 661.525 | 991.371 |
| active / cross_zero | {'ok': 8} | 803.176 | 803.176 |
| active / financed_buy | {'ok': 31} | 873.675 | 919.606 |
| active / sell_repay | {'ok': 32} | 849.476 | 981.519 |
| active / short_cover | {'ok': 30} | 955.491 | 961.517 |
| active / short_open | {'ok': 22} | 916.878 | 944.754 |
| normal / cash_buy | {'ok': 55} | 183.75 | 215.53 |
| normal / cash_sell | {'ok': 45} | 142.043 | 195.812 |
| normal / cross_zero | {'ok': 2} | 134.06 | 134.06 |
| normal / financed_buy | {'ok': 16} | 308.935 | 308.935 |
| normal / sell_repay | {'ok': 14} | 142.84 | 142.84 |
| normal / short_cover | {'ok': 11} | 86.091 | 86.091 |
| normal / short_open | {'ok': 12} | 232.001 | 232.001 |
| readheavy / cash_buy | {'ok': 20} | 69.427 | 74.919 |
| readheavy / financed_buy | {'ok': 6} | 106.44 | 106.44 |
| readheavy / short_open | {'ok': 4} | 99.571 | 99.571 |
| role_prepare / cash_buy | {'ok': 5} | 36.716 | 36.716 |
| role_prepare / cross_zero | {'ok': 5} | 58.605 | 58.605 |
| role_prepare / financed_buy | {'ok': 20} | 70.251 | 211.499 |
| role_prepare / sell_repay | {'ok': 20} | 50.06 | 55.475 |
| role_prepare / short_cover | {'ok': 20} | 38.864 | 39.734 |
| role_prepare / short_open | {'ok': 15} | 66.226 | 66.226 |
| smoke / cash_buy | {'ok': 4} | 89.965 | 89.965 |
| smoke / cash_sell | {'ok': 4} | 53.392 | 53.392 |
| stress / cash_buy | {'ok': 108} | 6616.967 | 7137.802 |
| stress / cash_sell | {'ok': 100} | 6312.362 | 6966.778 |
| stress / cross_zero | {'ok': 6} | 10317.67 | 10317.67 |
| stress / financed_buy | {'ok': 29} | 6518.68 | 7148.81 |
| stress / sell_repay | {'ok': 23} | 10396.357 | 10495.597 |
| stress / short_cover | {'ok': 24} | 6476.521 | 6587.08 |
| stress / short_open | {'ok': 23} | 10564.435 | 10666.916 |

Coverage: `{"chart_sse_tail_reused": 1034, "cash_buy": 304, "cash_sell": 247, "financed_buy": 102, "sell_repay": 89, "cross_zero": 21, "short_cover": 85, "short_open": 76}`

See summary.json and requests.jsonl for errors and sampling boundaries.
