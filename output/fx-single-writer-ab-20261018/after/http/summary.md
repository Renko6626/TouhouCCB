# FX load-test report

Target: http://127.0.0.1:18543
Abort: 30s window: successful trade P99 exceeds 2000ms

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.727 | 3.726 |
| readheavy | 30 | 0.499 | 22.476 |
| readonly | 0 | 0.0 | 6.451 |
| normal | 70 | 2.529 | 19.186 |
| active | 90 | 7.066 | 42.052 |
| stress | 100 | 10.909 | 60.659 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| active / cash_buy | {'ok': 112} | 569.193 | 624.402 |
| active / cash_sell | {'ok': 94} | 583.286 | 705.744 |
| active / cross_zero | {'ok': 7} | 231.228 | 231.228 |
| active / financed_buy | {'ok': 31} | 659.105 | 687.642 |
| active / sell_repay | {'ok': 30} | 455.57 | 573.557 |
| active / short_cover | {'ok': 30} | 512.968 | 560.032 |
| active / short_open | {'ok': 23} | 420.487 | 435.325 |
| normal / cash_buy | {'ok': 55} | 173.407 | 258.859 |
| normal / cash_sell | {'ok': 45} | 164.099 | 268.748 |
| normal / cross_zero | {'ok': 2} | 129.63 | 129.63 |
| normal / financed_buy | {'ok': 16} | 187.724 | 187.724 |
| normal / sell_repay | {'ok': 14} | 171.422 | 171.422 |
| normal / short_cover | {'ok': 11} | 112.129 | 112.129 |
| normal / short_open | {'ok': 12} | 142.527 | 142.527 |
| readheavy / cash_buy | {'ok': 20} | 104.354 | 107.91 |
| readheavy / financed_buy | {'ok': 6} | 114.477 | 114.477 |
| readheavy / short_open | {'ok': 4} | 108.895 | 108.895 |
| role_prepare / cash_buy | {'ok': 5} | 68.729 | 68.729 |
| role_prepare / cross_zero | {'ok': 5} | 73.025 | 73.025 |
| role_prepare / financed_buy | {'ok': 20} | 254.462 | 305.3 |
| role_prepare / sell_repay | {'ok': 20} | 73.132 | 80.938 |
| role_prepare / short_cover | {'ok': 20} | 67.362 | 83.069 |
| role_prepare / short_open | {'ok': 15} | 90.952 | 90.952 |
| smoke / cash_buy | {'ok': 4} | 85.557 | 85.557 |
| smoke / cash_sell | {'ok': 4} | 57.509 | 57.509 |
| stress / cash_buy | {'ok': 121} | 9939.122 | 10001.807 |
| stress / cash_sell | {'ok': 117} | 9957.252 | 10028.579 |
| stress / cross_zero | {'ok': 6} | 9871.633 | 9871.633 |
| stress / financed_buy | {'ok': 41} | 9937.697 | 10021.314 |
| stress / sell_repay | {'ok': 37} | 9959.476 | 9987.737 |
| stress / short_cover | {'ok': 33} | 9912.227 | 9943.375 |
| stress / short_open | {'ok': 27} | 10016.222 | 10051.278 |

Coverage: `{"chart_sse_tail_reused": 1097, "cash_buy": 317, "cash_sell": 260, "financed_buy": 114, "sell_repay": 101, "cross_zero": 20, "short_cover": 94, "short_open": 81}`

See summary.json and requests.jsonl for errors and sampling boundaries.
