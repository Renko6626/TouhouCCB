# FX load-test report

Target: http://127.0.0.1:18543
Abort: 30s window: successful trade P99 exceeds 2000ms

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.727 | 3.727 |
| readonly | 0 | 0.0 | 6.451 |
| normal | 70 | 2.459 | 18.966 |
| active | 90 | 7.315 | 43.997 |
| stress | 100 | 9.723 | 59.65 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| active / cash_buy | {'ok': 108} | 469.061 | 687.03 |
| active / cash_sell | {'ok': 109} | 560.07 | 767.395 |
| active / cross_zero | {'ok': 6} | 464.435 | 464.435 |
| active / financed_buy | {'ok': 33} | 707.852 | 782.11 |
| active / sell_repay | {'ok': 34} | 537.229 | 1037.148 |
| active / short_cover | {'ok': 27} | 438.476 | 580.126 |
| active / short_open | {'ok': 22} | 373.469 | 682.246 |
| normal / cash_buy | {'ok': 66} | 159.634 | 267.062 |
| normal / cash_sell | {'ok': 30} | 118.083 | 282.266 |
| normal / cross_zero | {'ok': 2} | 81.554 | 81.554 |
| normal / financed_buy | {'ok': 20} | 195.518 | 435.892 |
| normal / sell_repay | {'ok': 8} | 145.741 | 145.741 |
| normal / short_cover | {'ok': 8} | 112.527 | 112.527 |
| normal / short_open | {'ok': 16} | 298.956 | 298.956 |
| role_prepare / cash_buy | {'ok': 5} | 31.194 | 31.194 |
| role_prepare / cross_zero | {'ok': 5} | 64.138 | 64.138 |
| role_prepare / financed_buy | {'ok': 20} | 60.432 | 85.195 |
| role_prepare / sell_repay | {'ok': 20} | 44.424 | 55.457 |
| role_prepare / short_cover | {'ok': 20} | 31.965 | 33.388 |
| role_prepare / short_open | {'ok': 15} | 183.627 | 183.627 |
| smoke / cash_buy | {'ok': 4} | 91.894 | 91.894 |
| smoke / cash_sell | {'ok': 4} | 41.563 | 41.563 |
| stress / cash_buy | {'ok': 136} | 9220.614 | 9537.716 |
| stress / cash_sell | {'ok': 132} | 9284.696 | 9440.385 |
| stress / cross_zero | {'ok': 4, 'timeout': 2, 'version_conflict': 1} | 14348.671 | 14348.671 |
| stress / financed_buy | {'ok': 12, 'timeout': 8, 'version_conflict': 5} | 14966.508 | 14966.508 |
| stress / sell_repay | {'ok': 15, 'timeout': 6, 'version_conflict': 4} | 14338.364 | 14338.364 |
| stress / short_cover | {'ok': 26} | 8870.844 | 9292.268 |
| stress / short_open | {'ok': 16, 'timeout': 7, 'version_conflict': 6} | 14902.245 | 14902.245 |

Coverage: `{"chart_sse_tail_reused": 1033, "cash_buy": 319, "cash_sell": 275, "financed_buy": 85, "sell_repay": 77, "cross_zero": 17, "short_cover": 81, "short_open": 69}`

See summary.json and requests.jsonl for errors and sampling boundaries.
