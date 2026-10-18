# FX load-test report

Target: http://127.0.0.1:18543
Abort: 30s window: 429/5xx/network/timeout rate exceeds 5%

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.727 | 3.726 |
| readheavy | 30 | 0.499 | 22.463 |
| readonly | 0 | 0.0 | 6.451 |
| normal | 70 | 2.574 | 19.409 |
| active | 90 | 7.046 | 41.955 |
| stress | 100 | 8.051 | 52.95 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| active / cash_buy | {'ok': 109} | 1081.84 | 1436.894 |
| active / cash_sell | {'ok': 96} | 852.342 | 1547.452 |
| active / cross_zero | {'ok': 7} | 986.681 | 986.681 |
| active / financed_buy | {'ok': 30} | 557.891 | 782.843 |
| active / sell_repay | {'ok': 31} | 627.4 | 1142.98 |
| active / short_cover | {'ok': 30} | 907.631 | 978.615 |
| active / short_open | {'ok': 23} | 493.351 | 758.833 |
| normal / cash_buy | {'ok': 54} | 162.777 | 212.114 |
| normal / cash_sell | {'ok': 45} | 157.586 | 171.721 |
| normal / cross_zero | {'ok': 3} | 84.318 | 84.318 |
| normal / financed_buy | {'ok': 17} | 217.539 | 217.539 |
| normal / sell_repay | {'ok': 14} | 126.38 | 126.38 |
| normal / short_cover | {'ok': 11} | 120.271 | 120.271 |
| normal / short_open | {'ok': 13} | 214.538 | 214.538 |
| readheavy / cash_buy | {'ok': 20} | 107.786 | 111.423 |
| readheavy / financed_buy | {'ok': 6} | 208.224 | 208.224 |
| readheavy / short_open | {'ok': 4} | 111.252 | 111.252 |
| role_prepare / cash_buy | {'ok': 5} | 61.666 | 61.666 |
| role_prepare / cross_zero | {'ok': 5} | 84.197 | 84.197 |
| role_prepare / financed_buy | {'ok': 20} | 108.575 | 245.271 |
| role_prepare / sell_repay | {'ok': 20} | 73.488 | 74.921 |
| role_prepare / short_cover | {'ok': 20} | 68.909 | 71.885 |
| role_prepare / short_open | {'ok': 15} | 75.793 | 75.793 |
| smoke / cash_buy | {'ok': 4} | 94.045 | 94.045 |
| smoke / cash_sell | {'ok': 4} | 49.478 | 49.478 |
| stress / cash_buy | {'ok': 99, 'server_error': 23} | 10055.013 | 10296.427 |
| stress / cash_sell | {'ok': 81, 'server_error': 24} | 10022.154 | 10079.068 |
| stress / cross_zero | {'ok': 4, 'server_error': 2} | 9942.966 | 9942.966 |
| stress / financed_buy | {'ok': 24, 'server_error': 8} | 10102.461 | 10184.378 |
| stress / sell_repay | {'ok': 28, 'server_error': 8, 'business_rejected': 1} | 9930.659 | 10069.039 |
| stress / short_cover | {'ok': 26, 'server_error': 5} | 10057.582 | 10087.659 |
| stress / short_open | {'ok': 19, 'server_error': 4} | 10115.786 | 10115.786 |

Coverage: `{"chart_sse_tail_reused": 997, "cash_buy": 291, "cash_sell": 226, "financed_buy": 97, "sell_repay": 93, "cross_zero": 19, "short_cover": 87, "short_open": 74}`

See summary.json and requests.jsonl for errors and sampling boundaries.
