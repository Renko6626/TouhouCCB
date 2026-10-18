# FX load-test report

Target: http://127.0.0.1:18543
Abort: none

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.727 | 3.726 |
| readonly | 0 | 0.0 | 3.225 |
| normal | 35 | 1.213 | 9.36 |
| active | 45 | 3.677 | 21.675 |
| stress | 50 | 11.685 | 62.191 |
| burst | 50 | 4.544 | 21.085 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| active / cash_buy | {'ok': 35} | 123.02 | 261.865 |
| active / cash_sell | {'ok': 36} | 151.284 | 161.607 |
| active / cross_zero | {'ok': 2} | 98.323 | 98.323 |
| active / financed_buy | {'ok': 9} | 130.094 | 130.094 |
| active / sell_repay | {'ok': 14} | 224.786 | 224.786 |
| active / short_cover | {'ok': 11} | 75.164 | 75.164 |
| active / short_open | {'ok': 7} | 116.696 | 116.696 |
| burst / cash_buy | {'ok': 25} | 1023.261 | 1474.652 |
| burst / cash_sell | {'ok': 21} | 1027.646 | 1055.937 |
| burst / sell_repay | {'ok': 4} | 1904.586 | 1904.586 |
| burst_prepare / cash_buy | {'ok': 15} | 20.77 | 20.77 |
| burst_prepare / cash_sell | {} | — | — |
| burst_prepare / sell_repay | {} | — | — |
| burst_prepare / short_cover | {'ok': 5} | 49.58 | 49.58 |
| normal / cash_buy | {'ok': 33} | 56.384 | 83.057 |
| normal / cash_sell | {'ok': 15} | 50.613 | 50.613 |
| normal / cross_zero | {'ok': 1} | 36.241 | 36.241 |
| normal / financed_buy | {'ok': 11} | 102.168 | 102.168 |
| normal / sell_repay | {'ok': 4} | 151.931 | 151.931 |
| normal / short_cover | {'ok': 3} | 65.819 | 65.819 |
| normal / short_open | {'ok': 7} | 73.013 | 73.013 |
| role_prepare / cash_buy | {'ok': 3} | 20.726 | 20.726 |
| role_prepare / cross_zero | {'ok': 3} | 40.56 | 40.56 |
| role_prepare / financed_buy | {'ok': 10} | 44.577 | 44.577 |
| role_prepare / sell_repay | {'ok': 10} | 42.023 | 42.023 |
| role_prepare / short_cover | {'ok': 10} | 28.307 | 28.307 |
| role_prepare / short_open | {'ok': 7} | 84.257 | 84.257 |
| smoke / cash_buy | {'ok': 4} | 91.242 | 91.242 |
| smoke / cash_sell | {'ok': 4} | 49.85 | 49.85 |
| stress / cash_buy | {'ok': 120} | 567.463 | 659.525 |
| stress / cash_sell | {'ok': 113} | 617.137 | 746.63 |
| stress / cross_zero | {'ok': 8} | 1014.277 | 1014.277 |
| stress / financed_buy | {'ok': 35} | 993.993 | 1240.218 |
| stress / sell_repay | {'ok': 32} | 1078.321 | 1145.517 |
| stress / short_cover | {'ok': 32} | 540.204 | 574.328 |
| stress / short_open | {'ok': 26} | 1030.999 | 1182.44 |

Coverage: `{"chart_sse_tail_reused": 730, "cash_buy": 235, "cash_sell": 189, "financed_buy": 65, "sell_repay": 64, "cross_zero": 14, "short_cover": 61, "short_open": 47}`

See summary.json and requests.jsonl for errors and sampling boundaries.
