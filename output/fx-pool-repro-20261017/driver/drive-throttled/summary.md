# FX load-test report

Target: http://127.0.0.1:55571
Abort: 30s window: 429/5xx/network/timeout rate exceeds 5%

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.095 | 0.524 |
| readonly | 0 | 0.0 | 6.438 |
| normal | 55 | 0.356 | 6.216 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| normal / cash_buy | {'ok': 3, 'timeout': 1} | 2620.996 | 2620.996 |
| normal / cash_sell | {'ok': 12, 'timeout': 3} | 3676.943 | 3676.943 |
| normal / cross_zero | {} | — | — |
| normal / financed_buy | {'ok': 3, 'timeout': 3} | 4915.508 | 4915.508 |
| normal / sell_repay | {} | — | — |
| normal / short_cover | {} | — | — |
| normal / short_open | {'ok': 2, 'timeout': 2} | 5397.989 | 5397.989 |
| role_prepare / cash_buy | {'ok': 5} | 61.451 | 61.451 |
| role_prepare / cash_sell | {'ok': 7} | 66.638 | 66.638 |
| role_prepare / cross_zero | {'ok': 5} | 596.597 | 596.597 |
| role_prepare / financed_buy | {'ok': 20} | 184.115 | 212.296 |
| role_prepare / sell_repay | {'ok': 32} | 161.168 | 492.125 |
| role_prepare / short_cover | {'ok': 25} | 105.123 | 107.301 |
| role_prepare / short_open | {'ok': 15} | 657.666 | 657.666 |
| smoke / cash_buy | {'ok': 1} | 63.04 | 63.04 |
| smoke / cash_sell | {'ok': 1} | 105.608 | 105.608 |

Coverage: `{"chart_sse_tail_reused": 231, "cash_sell": 20, "cash_buy": 9, "sell_repay": 32, "financed_buy": 23, "short_cover": 25, "cross_zero": 5, "short_open": 17}`

See summary.json and requests.jsonl for errors and sampling boundaries.
