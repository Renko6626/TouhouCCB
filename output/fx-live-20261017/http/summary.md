# FX load-test report

Target: https://thccb.secret-sealing.club
Abort: 30s window: 429/5xx/network/timeout rate exceeds 5%

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.115 | 0.639 |
| readonly | 0 | 0.0 | 6.52 |
| normal | 58 | 0.19 | 5.208 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| normal / cash_buy | {'ok': 6, 'timeout': 8} | 2255.947 | 2255.947 |
| normal / cash_sell | {'ok': 1} | 207.466 | 207.466 |
| normal / financed_buy | {'ok': 2, 'timeout': 1} | 4421.663 | 4421.663 |
| normal / sell_repay | {} | — | — |
| normal / short_cover | {} | — | — |
| normal / short_open | {'ok': 1, 'timeout': 2} | 555.629 | 555.629 |
| role_prepare / cash_buy | {'ok': 5} | 230.578 | 230.578 |
| role_prepare / cross_zero | {'ok': 5} | 370.406 | 370.406 |
| role_prepare / financed_buy | {'ok': 20} | 465.762 | 490.921 |
| role_prepare / sell_repay | {'ok': 20} | 344.983 | 349.696 |
| role_prepare / short_cover | {'ok': 20} | 257.126 | 257.444 |
| role_prepare / short_open | {'ok': 15} | 651.897 | 651.897 |
| smoke / cash_buy | {'ok': 4} | 172.869 | 172.869 |
| smoke / cash_sell | {'ok': 3} | 133.432 | 133.432 |

Coverage: `{"chart_sse_tail_reused": 202, "cash_buy": 15, "cash_sell": 4, "financed_buy": 22, "sell_repay": 20, "cross_zero": 5, "short_cover": 20, "short_open": 16}`

See summary.json and requests.jsonl for errors and sampling boundaries.
