# FX load-test report

Target: https://thccb.secret-sealing.club
Abort: 30s window: successful trade P99 exceeds 2000ms

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.131 | 0.721 |
| readonly | 0 | 0.0 | 3.264 |
| normal | 32 | 1.271 | 9.964 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| normal / cash_buy | {'ok': 24} | 1413.607 | 1762.522 |
| normal / cash_sell | {'ok': 12} | 1064.711 | 1064.711 |
| normal / cross_zero | {'ok': 1} | 896.469 | 896.469 |
| normal / financed_buy | {'ok': 8} | 2259.433 | 2259.433 |
| normal / sell_repay | {'ok': 4} | 1955.278 | 1955.278 |
| normal / short_cover | {'ok': 3} | 229.716 | 229.716 |
| normal / short_open | {'ok': 4} | 528.039 | 528.039 |
| role_prepare / cash_buy | {'ok': 3} | 130.204 | 130.204 |
| role_prepare / cross_zero | {'ok': 3} | 208.145 | 208.145 |
| role_prepare / financed_buy | {'ok': 10} | 296.923 | 296.923 |
| role_prepare / sell_repay | {'ok': 10} | 287.349 | 287.349 |
| role_prepare / short_cover | {'ok': 10} | 274.011 | 274.011 |
| role_prepare / short_open | {'ok': 7} | 668.657 | 668.657 |
| smoke / cash_buy | {'ok': 4} | 219.171 | 219.171 |
| smoke / cash_sell | {'ok': 4} | 229.903 | 229.903 |

Coverage: `{"chart_sse_tail_reused": 157, "cash_buy": 31, "cash_sell": 16, "financed_buy": 18, "sell_repay": 14, "cross_zero": 4, "short_cover": 13, "short_open": 11}`

See summary.json and requests.jsonl for errors and sampling boundaries.
