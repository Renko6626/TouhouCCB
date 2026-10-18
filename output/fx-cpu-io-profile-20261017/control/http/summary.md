# FX load-test report

Target: http://127.0.0.1:18543
Abort: 30s window: successful trade P99 exceeds 2000ms

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.727 | 3.726 |
| readonly | 0 | 0.0 | 6.451 |
| normal | 70 | 2.537 | 19.349 |
| active | 90 | 7.167 | 44.718 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| active / cash_buy | {'ok': 67} | 1851.614 | 1997.861 |
| active / cash_sell | {'ok': 80} | 1031.229 | 1506.217 |
| active / cross_zero | {'ok': 4} | 2463.294 | 2463.294 |
| active / financed_buy | {'ok': 17} | 1907.356 | 1907.356 |
| active / sell_repay | {'ok': 26} | 1601.019 | 1716.882 |
| active / short_cover | {'ok': 22} | 1879.756 | 1901.877 |
| active / short_open | {'ok': 14} | 3371.101 | 3371.101 |
| normal / cash_buy | {'ok': 69} | 133.986 | 372.116 |
| normal / cash_sell | {'ok': 29} | 110.939 | 114.943 |
| normal / cross_zero | {'ok': 2} | 81.901 | 81.901 |
| normal / financed_buy | {'ok': 21} | 228.163 | 296.061 |
| normal / sell_repay | {'ok': 8} | 252.788 | 252.788 |
| normal / short_cover | {'ok': 10} | 140.345 | 140.345 |
| normal / short_open | {'ok': 16} | 182.585 | 182.585 |
| role_prepare / cash_buy | {'ok': 5} | 26.354 | 26.354 |
| role_prepare / cross_zero | {'ok': 5} | 60.597 | 60.597 |
| role_prepare / financed_buy | {'ok': 20} | 49.833 | 242.292 |
| role_prepare / sell_repay | {'ok': 20} | 49.764 | 51.724 |
| role_prepare / short_cover | {'ok': 20} | 31.034 | 32.664 |
| role_prepare / short_open | {'ok': 15} | 55.685 | 55.685 |
| smoke / cash_buy | {'ok': 4} | 58.265 | 58.265 |
| smoke / cash_sell | {'ok': 4} | 57.979 | 57.979 |

Coverage: `{"chart_sse_tail_reused": 588, "cash_buy": 145, "cash_sell": 113, "financed_buy": 58, "sell_repay": 54, "cross_zero": 11, "short_cover": 52, "short_open": 45}`

See summary.json and requests.jsonl for errors and sampling boundaries.
