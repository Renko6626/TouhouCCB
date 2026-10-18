# FX load-test report

Target: http://127.0.0.1:18543
Abort: 30s window: successful trade P99 exceeds 2000ms

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.727 | 3.726 |
| readheavy | 30 | 0.5 | 22.496 |
| readonly | 0 | 0.0 | 6.451 |
| normal | 70 | 2.557 | 19.343 |
| active | 90 | 7.025 | 41.912 |
| stress | 100 | 10.11 | 56.536 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| active / cash_buy | {'ok': 113} | 506.471 | 639.906 |
| active / cash_sell | {'ok': 93} | 617.704 | 667.442 |
| active / cross_zero | {'ok': 7} | 382.062 | 382.062 |
| active / financed_buy | {'ok': 27} | 759.373 | 773.705 |
| active / sell_repay | {'ok': 29} | 669.106 | 909.779 |
| active / short_cover | {'ok': 31} | 400.328 | 577.124 |
| active / short_open | {'ok': 24} | 618.186 | 853.247 |
| normal / cash_buy | {'ok': 54} | 185.761 | 293.099 |
| normal / cash_sell | {'ok': 45} | 205.664 | 417.768 |
| normal / cross_zero | {'ok': 2} | 134.137 | 134.137 |
| normal / financed_buy | {'ok': 17} | 173.162 | 173.162 |
| normal / sell_repay | {'ok': 14} | 199.556 | 199.556 |
| normal / short_cover | {'ok': 12} | 75.895 | 75.895 |
| normal / short_open | {'ok': 12} | 171.948 | 171.948 |
| readheavy / cash_buy | {'ok': 20} | 75.074 | 83.984 |
| readheavy / financed_buy | {'ok': 6} | 105.641 | 105.641 |
| readheavy / short_open | {'ok': 4} | 101.09 | 101.09 |
| role_prepare / cash_buy | {'ok': 5} | 34.212 | 34.212 |
| role_prepare / cross_zero | {'ok': 5} | 71.246 | 71.246 |
| role_prepare / financed_buy | {'ok': 20} | 205.808 | 262.029 |
| role_prepare / sell_repay | {'ok': 20} | 59.604 | 64.037 |
| role_prepare / short_cover | {'ok': 20} | 44.769 | 45.195 |
| role_prepare / short_open | {'ok': 15} | 84.503 | 84.503 |
| smoke / cash_buy | {'ok': 4} | 71.413 | 71.413 |
| smoke / cash_sell | {'ok': 4} | 55.33 | 55.33 |
| stress / cash_buy | {'ok': 112} | 5446.902 | 5633.791 |
| stress / cash_sell | {'ok': 113} | 5485.052 | 5675.865 |
| stress / cross_zero | {'ok': 5} | 9329.979 | 9329.979 |
| stress / financed_buy | {'ok': 32} | 5445.45 | 5499.042 |
| stress / sell_repay | {'ok': 29} | 9829.998 | 9857.691 |
| stress / short_cover | {'ok': 26} | 5503.858 | 5653.92 |
| stress / short_open | {'ok': 24} | 9816.311 | 9892.198 |

Coverage: `{"chart_sse_tail_reused": 1054, "cash_buy": 308, "cash_sell": 255, "financed_buy": 102, "sell_repay": 92, "cross_zero": 19, "short_cover": 89, "short_open": 79}`

See summary.json and requests.jsonl for errors and sampling boundaries.
