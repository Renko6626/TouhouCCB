# FX load-test report

Target: http://127.0.0.1:18543
Abort: 30s window: successful trade P99 exceeds 2000ms

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.727 | 3.727 |
| readheavy | 30 | 0.5 | 22.48 |
| readonly | 0 | 0.0 | 6.451 |
| normal | 70 | 2.524 | 19.327 |
| active | 90 | 7.124 | 43.089 |
| stress | 100 | 9.537 | 59.64 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| active / cash_buy | {'ok': 109} | 564.808 | 689.604 |
| active / cash_sell | {'ok': 97} | 488.343 | 744.65 |
| active / cross_zero | {'ok': 6} | 1022.86 | 1022.86 |
| active / financed_buy | {'ok': 34} | 665.162 | 817.665 |
| active / sell_repay | {'ok': 31} | 618.727 | 679.583 |
| active / short_cover | {'ok': 30} | 527.604 | 706.377 |
| active / short_open | {'ok': 24} | 690.494 | 1175.495 |
| normal / cash_buy | {'ok': 54} | 128.679 | 214.098 |
| normal / cash_sell | {'ok': 44} | 122.961 | 160.284 |
| normal / cross_zero | {'ok': 2} | 173.779 | 173.779 |
| normal / financed_buy | {'ok': 17} | 236.441 | 236.441 |
| normal / sell_repay | {'ok': 14} | 153.579 | 153.579 |
| normal / short_cover | {'ok': 11} | 66.319 | 66.319 |
| normal / short_open | {'ok': 12} | 251.808 | 251.808 |
| readheavy / cash_buy | {'ok': 20} | 52.619 | 61.293 |
| readheavy / financed_buy | {'ok': 6} | 87.546 | 87.546 |
| readheavy / short_open | {'ok': 4} | 75.745 | 75.745 |
| role_prepare / cash_buy | {'ok': 5} | 24.922 | 24.922 |
| role_prepare / cross_zero | {'ok': 5} | 58.545 | 58.545 |
| role_prepare / financed_buy | {'ok': 20} | 46.274 | 58.724 |
| role_prepare / sell_repay | {'ok': 20} | 52.315 | 67.06 |
| role_prepare / short_cover | {'ok': 20} | 31.646 | 34.921 |
| role_prepare / short_open | {'ok': 15} | 47.7 | 47.7 |
| smoke / cash_buy | {'ok': 4} | 90.957 | 90.957 |
| smoke / cash_sell | {'ok': 4} | 52.256 | 52.256 |
| stress / cash_buy | {'ok': 130} | 9380.075 | 9577.438 |
| stress / cash_sell | {'ok': 125} | 9437.851 | 9591.176 |
| stress / cross_zero | {'ok': 6, 'timeout': 1} | 13100.448 | 13100.448 |
| stress / financed_buy | {'ok': 20} | 9407.178 | 9436.792 |
| stress / sell_repay | {'ok': 15, 'timeout': 13, 'version_conflict': 12, 'business_rejected': 1} | 14472.67 | 14472.67 |
| stress / short_cover | {'ok': 21} | 9093.695 | 9544.015 |
| stress / short_open | {'ok': 14, 'timeout': 8, 'version_conflict': 5} | 14163.17 | 14163.17 |

Coverage: `{"chart_sse_tail_reused": 1049, "cash_buy": 322, "cash_sell": 270, "financed_buy": 97, "sell_repay": 80, "cross_zero": 19, "short_cover": 82, "short_open": 69}`

See summary.json and requests.jsonl for errors and sampling boundaries.
